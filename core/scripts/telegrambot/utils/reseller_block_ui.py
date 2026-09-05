"""Telegram controls for the shared durable reseller block service."""

from telebot import types
import uuid
from utils.reseller_experience import experience_text
from utils.time_utils import utc_now, parse_utc_timestamp, format_utc_display


def add_block_button(markup, reseller_id, config_index, language):
    from utils.reseller_blocks import customer_block_token
    try:
        token = customer_block_token(reseller_id, config_index)
    except ValueError:
        return markup
    shown = set(getattr(markup, '_reseller_block_tokens', ()))
    if token in shown:
        return markup
    markup._reseller_block_tokens = shown | {token}
    markup.add(types.InlineKeyboardButton(experience_text(language, 'block_button'), callback_data=f'rb:{token}:view'))
    return markup


def block_status_text(language, config, *, now=None):
    block = config.get('reseller_block') or {}
    if not block:
        return ''
    if block.get('state') == 'complete':
        return experience_text(language, 'complete') + (
            '\n' + experience_text(language, 'other_block') if block.get('other_block') else '')
    deadline = parse_utc_timestamp(block.get('until'))
    if not deadline:
        return experience_text(language, 'failed')
    current = parse_utc_timestamp(now) if now is not None else utc_now()
    state = 'blocked' if block.get('state') == 'blocked' and not block.get('last_error') else 'pending'
    return experience_text(language, 'block_status', date=format_utc_display(deadline),
        hours=f'{max(0, (deadline-current).total_seconds()/3600):.1f}', state=experience_text(language, state))


def register_block_handlers(bot, language_for, multi_api_factory, *, owner_id=None):
    def identity(user_id):
        if owner_id is not None and user_id != owner_id:
            raise ValueError('denied')
        return user_id if owner_id is None else owner_id

    def render(chat_id, reseller_id, token, language):
        from utils.reseller_blocks import block_view, ACTIVE_STATES
        config = block_view(reseller_id, token)
        markup = types.InlineKeyboardMarkup(row_width=2)
        if (config.get('reseller_block') or {}).get('state') in ACTIVE_STATES:
            block_id = config['reseller_block']['id']
            markup.add(types.InlineKeyboardButton(experience_text(language, 'unblock_button'), callback_data=f'rb:{token}:release:{block_id}'))
        else:
            request_id = uuid.uuid4().hex[:12]
            for hours in (1, 6, 24, 168):
                markup.add(types.InlineKeyboardButton(experience_text(language, 'hours', hours=hours), callback_data=f'rb:{token}:{hours}:{request_id}'))
            markup.add(types.InlineKeyboardButton(experience_text(language, 'custom'), callback_data=f'rb:{token}:custom:{request_id}'))
        markup.add(types.InlineKeyboardButton(experience_text(language, 'cancel'), callback_data=f'rb:{token}:cancel'))
        text = f"{config['username']}\n" + experience_text(language, 'block_prompt')
        status = block_status_text(language, config)
        bot.send_message(chat_id, text + ('\n\n' + status if status else ''), reply_markup=markup)

    def custom_input(message, reseller_id, token, request_id):
        language = language_for(message.from_user.id)
        try:
            if identity(message.from_user.id) != reseller_id:
                raise ValueError('denied')
            if (message.text or '').strip() == '/cancel':
                bot.reply_to(message, experience_text(language, 'cancel'))
                return
            raw = (message.text or '').strip()
            if not raw.isdecimal() or not 1 <= int(raw) <= 720:
                prompt = bot.reply_to(message, experience_text(language, 'invalid'))
                bot.register_next_step_handler(prompt, custom_input, reseller_id, token, request_id)
                return
            from utils.reseller_blocks import request_block
            request_block(reseller_id, token, int(raw), multi_api_factory(), request_id=request_id)
            render(message.chat.id, reseller_id, token, language)
        except ValueError:
            bot.reply_to(message, experience_text(language, 'denied'))
        except Exception:
            bot.reply_to(message, experience_text(language, 'failed'))

    @bot.callback_query_handler(func=lambda call: call.data.startswith('rb:'))
    def block_callback(call):
        language = language_for(call.from_user.id)
        try:
            reseller_id = identity(call.from_user.id)
            parts = call.data.split(':')
            _, token, action = parts[:3]
            request_id = parts[3] if len(parts) == 4 else None
            from utils.reseller_blocks import block_view, request_block, release_block
            block_view(reseller_id, token)
            bot.answer_callback_query(call.id)
            if action == 'cancel':
                bot.clear_step_handler_by_chat_id(call.message.chat.id)
                bot.send_message(call.message.chat.id, experience_text(language, 'cancel'))
                return
            if action == 'custom':
                prompt = bot.send_message(call.message.chat.id, experience_text(language, 'custom_prompt'))
                bot.register_next_step_handler(prompt, custom_input, reseller_id, token, request_id)
                return
            if action == 'release':
                if not request_id:
                    raise ValueError('denied')
                release_block(reseller_id, token, multi_api_factory(), expected_block_id=request_id)
            elif action != 'view':
                if not request_id:
                    raise ValueError('denied')
                request_block(reseller_id, token, int(action), multi_api_factory(), request_id=request_id)
            render(call.message.chat.id, reseller_id, token, language)
        except ValueError:
            bot.send_message(call.message.chat.id, experience_text(language, 'denied'))
        except Exception:
            bot.send_message(call.message.chat.id, experience_text(language, 'failed'))

    return block_callback
