"""
Router of the “Tariff Groups” section.

Processes:
- List of groups
- Adding a group
- Renaming a group
- Deleting a group (with transfer of tariffs/servers to “Main”)
- Sorting (⬆️ swap with previous)
"""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from database.requests import (
    get_all_groups,
    get_group_by_id,
    add_group,
    update_group_name,
    delete_group,
    move_group_up,
    get_groups_count,
    get_tariffs_by_group,
    get_active_servers_by_group,
    toggle_group_monthly_traffic_reset,
    set_group_subscription_parent,
)
from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.keyboards.admin import (
    groups_list_kb,
    group_view_kb,
    group_subscription_parent_kb,
    group_delete_confirm_kb,
    back_and_home_kb
)
from bot.utils.text import escape_html, safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()


def _group_subscription_parent_id(group: dict) -> int | None:
    """Return the optional subscription host group id from a DB row."""
    value = group.get('subscription_parent_group_id')
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _group_subscription_parent_name(group: dict, groups: list[dict]) -> str | None:
    parent_id = _group_subscription_parent_id(group)
    if parent_id is None:
        return None
    for candidate in groups:
        if int(candidate['id']) == parent_id:
            return str(candidate.get('name') or parent_id)
    return str(parent_id)


def _build_group_view_text(
    group: dict,
    tariffs: list[dict],
    servers: list[dict],
    groups: list[dict],
    *,
    notice: str | None = None,
) -> str:
    """Build the administrator group card from current database state."""
    from bot.services.money import format_money_minor

    group_id = int(group['id'])
    is_default = " <i>(по умолчанию)</i>" if group_id == 1 else ""
    parent_name = _group_subscription_parent_name(group, groups)
    parent_text = (
        escape_html(parent_name)
        if parent_name is not None
        else "нет"
    )
    text = ""
    if notice:
        text += f"{notice}\n\n"
    text += (
        f"📂 <b>{escape_html(str(group.get('name') or group_id))}</b>{is_default}\n\n"
        f"🔢 Порядок: {int(group.get('sort_order') or 0)}\n"
        f"📋 Активных тарифов: {len(tariffs)}\n"
        f"🖥️ Активных серверов: {len(servers)}\n"
        f"🔄 Автосброс 1-го числа: "
        f"{'включён' if group.get('monthly_traffic_reset_enabled') else 'выключен'}\n"
        f"🔗 Привязывать к подписке из группы: <b>{parent_text}</b>\n"
    )
    if tariffs:
        text += "\n<b>Тарифы:</b>\n"
        for tariff in tariffs:
            text += (
                f"  • {escape_html(str(tariff.get('name') or tariff['id']))} — "
                f"{format_money_minor(tariff.get('price_minor', 0), tariff.get('base_currency', 'RUB'))}\n"
            )
    if servers:
        text += "\n<b>Серверы:</b>\n"
        for server in servers:
            text += f"  • {escape_html(str(server.get('name') or server['id']))}\n"
    return text


async def _render_group_view(
    callback: CallbackQuery,
    group_id: int,
    *,
    notice: str | None = None,
) -> bool:
    group = get_group_by_id(group_id)
    if not group:
        return False
    tariffs = get_tariffs_by_group(group_id)
    servers = get_active_servers_by_group(group_id)
    await safe_edit_or_send(
        callback.message,
        _build_group_view_text(
            group,
            tariffs,
            servers,
            get_all_groups(),
            notice=notice,
        ),
        reply_markup=group_view_kb(
            group_id,
            bool(group.get('monthly_traffic_reset_enabled')),
        ),
    )
    return True


# ============================================================================
# LIST OF GROUPS
# ============================================================================

@router.callback_query(F.data == "admin_groups")
async def show_groups_list(callback: CallbackQuery, state: FSMContext):
    """Shows a list of tariff groups."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    await state.set_state(AdminStates.payments_menu)
    
    groups = get_all_groups()
    
    # We collect statistics for each group
    groups_info = []
    for group in groups:
        tariffs_count = len(get_tariffs_by_group(group['id']))
        servers_count = len(get_active_servers_by_group(group['id']))
        groups_info.append({
            **group,
            'tariffs_count': tariffs_count,
            'servers_count': servers_count
        })
    
    text = (
        "📂 <b>Группы тарифов</b>\n\n"
        "Группы ограничивают доступ: ключи можно продлевать и переносить "
        "только в рамках своей группы.\n\n"
    )
    
    if len(groups) == 1:
        text += (
            "ℹ️ Сейчас одна группа — ограничения не действуют.\n"
            "Добавьте вторую группу, чтобы разделить тарифы и серверы.\n"
        )
    
    for g in groups_info:
        is_default = " <i>(по умолчанию)</i>" if g['id'] == 1 else ""
        text += f"\n📂 <b>{escape_html(str(g['name']))}</b>{is_default}\n"
        text += f"   Тарифов: {g['tariffs_count']} | Серверов: {g['servers_count']}\n"
        reset_text = "включён" if g['monthly_traffic_reset_enabled'] else "выключен"
        text += f"   Автосброс: {reset_text}\n"
    
    await safe_edit_or_send(callback.message, 
        text,
        reply_markup=groups_list_kb(groups)
    )
    await callback.answer()


# ============================================================================
# ADDING A GROUP
# ============================================================================

@router.callback_query(F.data == "admin_group_add")
async def group_add_start(callback: CallbackQuery, state: FSMContext):
    """Starts adding a new group."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    await state.set_state(AdminStates.group_add_name)
    
    sent = await safe_edit_or_send(callback.message, 
        "📂 <b>Новая группа</b>\n\n"
        "⚠️ После добавления второй группы у пользователей появится "
        "разделение тарифов и серверов по группам.\n\n"
        "Введите название группы (макс. 30 символов):",
        reply_markup=back_and_home_kb("admin_groups")
    )
    await state.update_data(add_group_chat_id=callback.message.chat.id, add_group_message_id=callback.message.message_id)
    await callback.answer()


@router.message(AdminStates.group_add_name)
async def group_add_name_handler(message: Message, state: FSMContext):
    """Processes entering the name of a new group."""
    if not is_admin(message.from_user.id):
        return
    
    from bot.utils.text import get_message_text_for_storage, safe_edit_or_send
    name = get_message_text_for_storage(message, 'plain').strip()
    
    if not name or len(name) > 30:
        await safe_edit_or_send(message,
            "⚠️ Название должно быть от 1 до 30 символов."
        )
        return
    
    # Deleting a user's message
    try:
        await message.delete()
    except:
        pass
    
    # Create a group
    group_id = add_group(name)
    
    data = await state.get_data()
    add_chat_id = data.get('add_group_chat_id')
    add_msg_id = data.get('add_group_message_id')
    
    await state.set_state(AdminStates.payments_menu)
    
    # Collecting data to display a list of groups
    groups = get_all_groups()
    groups_info = []
    for group in groups:
        tariffs_count = len(get_tariffs_by_group(group['id']))
        servers_count = len(get_active_servers_by_group(group['id']))
        groups_info.append({
            **group,
            'tariffs_count': tariffs_count,
            'servers_count': servers_count
        })
    
    text = (
        f"✅ Группа <b>{name}</b> создана!\n\n"
        "📂 <b>Группы тарифов</b>\n\n"
        "Группы ограничивают доступ: ключи можно продлевать и переносить "
        "только в рамках своей группы.\n\n"
    )
    
    if len(groups) == 1:
        text += (
            "ℹ️ Сейчас одна группа — ограничения не действуют.\n"
            "Добавьте вторую группу, чтобы разделить тарифы и серверы.\n"
        )
    
    for g in groups_info:
        is_default = " _(по умолчанию)_" if g['id'] == 1 else ""
        text += f"\n📂 <b>{g['name']}</b>{is_default}\n"
        text += f"   Тарифов: {g['tariffs_count']} | Серверов: {g['servers_count']}\n"
    
    # Editing the original message with the form
    if add_chat_id and add_msg_id:
        try:
            from bot.keyboards.admin import groups_list_kb
            await message.bot.edit_message_text(
                text,
                chat_id=add_chat_id,
                message_id=add_msg_id,
                reply_markup=groups_list_kb(groups)
            )
        except Exception as e:
            logger.warning(f"Не удалось отредактировать сообщение: {e}")
            await safe_edit_or_send(message, text, reply_markup=groups_list_kb(groups), force_new=True)
    else:
        from bot.keyboards.admin import groups_list_kb
        await safe_edit_or_send(message, text, reply_markup=groups_list_kb(groups), force_new=True)


# ============================================================================
# VIEW/EDIT GROUP
# ============================================================================

@router.callback_query(F.data.startswith("admin_group_view:"))
async def group_view_handler(callback: CallbackQuery, state: FSMContext):
    """Shows information about the group."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    group_id = int(callback.data.split(":")[1])
    if not await _render_group_view(callback, group_id):
        await callback.answer("❌ Группа не найдена", show_alert=True)
        return
    await state.set_state(AdminStates.payments_menu)
    await callback.answer()


@router.callback_query(F.data.startswith('admin_group_parent:'))
async def group_subscription_parent_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Open the optional subscription host group selector."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        group_id = int(str(callback.data).split(':', 1)[1])
    except (TypeError, ValueError, IndexError):
        await callback.answer("❌ Группа не найдена", show_alert=True)
        return
    group = get_group_by_id(group_id)
    if not group:
        await callback.answer("❌ Группа не найдена", show_alert=True)
        return

    groups = get_all_groups()
    candidates = [
        candidate
        for candidate in groups
        if int(candidate['id']) != group_id
    ]
    current_parent_id = _group_subscription_parent_id(group)
    current_parent_name = _group_subscription_parent_name(group, groups)
    current_text = (
        f"<b>{escape_html(current_parent_name)}</b>"
        if current_parent_name is not None
        else "<b>не настроено</b>"
    )
    text = (
        "🔗 <b>Привязка к подписке</b>\n\n"
        f"Группа ключа: <b>{escape_html(str(group.get('name') or group_id))}</b>\n"
        f"Искать подписку в группе: {current_text}\n\n"
        "После покупки ключа из этой группы бот ищет у пользователя подходящие "
        "активные подписки в выбранной группе. Если подписка одна, ключ привязывается "
        "автоматически; если несколько — бот предложит пользователю выбрать. Если "
        "подходящей подписки нет, покупка всё равно завершится, а ключ останется "
        "отдельным.\n\n"
        "⚠️ Подписка для привязки должна находиться на сервере с 3X-UI 3.4.0 "
        "или новее; ключи на 3.3.x не участвуют в автоматическом выборе.\n\n"
        "Выберите группу, в которой бот будет искать подписку для привязки:"
    )

    await state.set_state(AdminStates.group_subscription_parent)
    await state.update_data(subscription_parent_group_id=group_id)
    await safe_edit_or_send(
        callback.message,
        text,
        reply_markup=group_subscription_parent_kb(
            candidates,
            current_parent_group_id=current_parent_id,
            back_callback=f'admin_group_view:{group_id}',
        ),
    )
    await callback.answer()


@router.callback_query(
    AdminStates.group_subscription_parent,
    F.data.startswith('admin_group_parent_set:'),
)
async def group_subscription_parent_set(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Persist the selected subscription host group through the DB facade."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    try:
        group_id = int(data.get('subscription_parent_group_id') or 0)
        selected_id = int(str(callback.data).rsplit(':', 1)[1])
    except (TypeError, ValueError, IndexError):
        await callback.answer("❌ Некорректный выбор", show_alert=True)
        return
    parent_group_id = selected_id if selected_id > 0 else None
    if group_id <= 0 or not get_group_by_id(group_id):
        await state.set_state(AdminStates.payments_menu)
        await callback.answer("❌ Группа не найдена", show_alert=True)
        return

    try:
        success = set_group_subscription_parent(
            group_id=group_id,
            parent_group_id=parent_group_id,
        )
    except ValueError as error:
        logger.warning(
            'Rejected subscription parent group=%s parent=%s: %s',
            group_id,
            parent_group_id,
            error,
        )
        success = False
    if not success:
        await callback.answer(
            "❌ Нельзя создать такую связь между группами",
            show_alert=True,
        )
        return

    await state.set_state(AdminStates.payments_menu)
    notice = (
        "✅ Привязка к подписке настроена"
        if parent_group_id is not None
        else "✅ Привязка к подписке отключена"
    )
    if not await _render_group_view(callback, group_id, notice=notice):
        await callback.answer("❌ Группа не найдена", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("admin_group_edit:"))
async def group_edit_start(callback: CallbackQuery, state: FSMContext):
    """Starts renaming the group."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    group_id = int(callback.data.split(":")[1])
    group = get_group_by_id(group_id)
    
    if not group:
        await callback.answer("❌ Группа не найдена", show_alert=True)
        return
    
    await state.set_state(AdminStates.group_edit_name)
    await state.update_data(edit_group_id=group_id, edit_message_id=callback.message.message_id)
    
    await safe_edit_or_send(callback.message, 
        f"✏️ <b>Переименование группы</b>\n\n"
        f"Текущее название: <b>{escape_html(str(group['name']))}</b>\n\n"
        "Введите новое название (макс. 30 символов):",
        reply_markup=back_and_home_kb(f"admin_group_view:{group_id}")
    )
    await callback.answer()


@router.message(AdminStates.group_edit_name)
async def group_edit_name_handler(message: Message, state: FSMContext):
    """Processes the entry of a new group name."""
    if not is_admin(message.from_user.id):
        return
    
    from bot.utils.text import get_message_text_for_storage, safe_edit_or_send
    name = get_message_text_for_storage(message, 'plain').strip()
    
    if not name or len(name) > 30:
        await safe_edit_or_send(message, "⚠️ Название должно быть от 1 до 30 символов.")
        return
    
    data = await state.get_data()
    group_id = data.get('edit_group_id')
    edit_msg_id = data.get('edit_message_id')
    
    if not group_id:
        await state.clear()
        await safe_edit_or_send(message, "❌ Ошибка состояния.")
        return
    
    # Deleting a user's message
    try:
        await message.delete()
    except:
        pass
    
    # Updating the name
    success = update_group_name(group_id, name)
    
    await state.set_state(AdminStates.payments_menu)
    
    if success and edit_msg_id:
        # Pattern: editing the original message
        group = get_group_by_id(group_id)
        tariffs = get_tariffs_by_group(group_id)
        servers = get_active_servers_by_group(group_id)
        
        text = _build_group_view_text(
            group,
            tariffs,
            servers,
            get_all_groups(),
            notice="✅ Группа переименована!",
        )
        
        try:
            await message.bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=edit_msg_id,
                reply_markup=group_view_kb(
                    group_id,
                    bool(group['monthly_traffic_reset_enabled']),
                )
            )
        except:
            await safe_edit_or_send(
                message,
                text,
                reply_markup=group_view_kb(
                    group_id,
                    bool(group['monthly_traffic_reset_enabled']),
                ),
                force_new=True,
            )
    else:
        await safe_edit_or_send(
            message,
            f"✅ Группа переименована в <b>{escape_html(name)}</b>",
        )


# ============================================================================
# DELETING A GROUP
# ============================================================================

@router.callback_query(F.data.startswith("admin_group_delete:"))
async def group_delete_start(callback: CallbackQuery, state: FSMContext):
    """Group deletion confirmation."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    group_id = int(callback.data.split(":")[1])
    
    if group_id == 1:
        await callback.answer("❌ Группу «Основная» нельзя удалить", show_alert=True)
        return
    
    group = get_group_by_id(group_id)
    if not group:
        await callback.answer("❌ Группа не найдена", show_alert=True)
        return
    
    tariffs = get_tariffs_by_group(group_id)
    servers = get_active_servers_by_group(group_id)
    
    text = (
        f"⚠️ <b>Удаление группы «{group['name']}»</b>\n\n"
        f"📋 Тарифов: {len(tariffs)}\n"
        f"🖥️ Серверов: {len(servers)}\n\n"
    )
    
    if tariffs or servers:
        text += "❗ Все тарифы и серверы будут перенесены в группу «Основная».\n\n"
    
    text += "Вы уверены?"
    
    await safe_edit_or_send(callback.message, 
        text,
        reply_markup=group_delete_confirm_kb(group_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_group_delete_confirm:"))
async def group_delete_confirm(callback: CallbackQuery, state: FSMContext):
    """Performs deletion of a group."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    group_id = int(callback.data.split(":")[1])
    
    success = delete_group(group_id)
    
    if success:
        await callback.answer("✅ Группа удалена, содержимое перенесено в «Основная»")
    else:
        await callback.answer("❌ Не удалось удалить группу", show_alert=True)
    
    # Returning to the list of groups
    await show_groups_list(callback, state)


# ============================================================================
# SORTING GROUPS (⬆️)
# ============================================================================

@router.callback_query(F.data.startswith("admin_group_up:"))
async def group_move_up_handler(callback: CallbackQuery, state: FSMContext):
    """Moves the group up in the sorting."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    group_id = int(callback.data.split(":")[1])
    
    move_group_up(group_id)
    await callback.answer("🔄 Порядок обновлён")
    
    # Updating the list
    await show_groups_list(callback, state)


@router.callback_query(F.data.startswith("admin_group_monthly_reset:"))
async def group_monthly_reset_toggle(callback: CallbackQuery, state: FSMContext):
    """Toggles monthly traffic reset for one tariff group."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    group_id = int(callback.data.split(":", 1)[1])
    enabled = toggle_group_monthly_traffic_reset(group_id)
    if enabled is None:
        await callback.answer("❌ Группа не найдена", show_alert=True)
        return
    forwarded = callback.model_copy(
        update={'data': f'admin_group_view:{group_id}'},
    )
    await group_view_handler(forwarded, state)
