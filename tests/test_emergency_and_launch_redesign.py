import time
from unittest.mock import MagicMock, patch
import pytest

from vds_bot.telegram_ui import TelegramBotApi
from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
from vds_bot.routers.power_router import route_power_callback


class MockApp:
    def __init__(self):
        self.ui = TelegramBotApi('mock_token')
        self.ui.send_message = MagicMock(return_value={'message_id': 1001})
        self.ui.answer_callback = MagicMock()
        self.mqtt = MagicMock()
        self.mqtt.esp_status = 'online'
        self.mqtt.system_status = 'offline'
        self.mqtt.system_session = 'unknown'
        self.mqtt.desktop_status = 'offline'
        self.power_flow = PowerFlowManager(self.ui, self.mqtt)
        self.power_flow.message_id = 1001
        self.power_flow.chat_id = 123456
        self.save_state = MagicMock()


def test_launch_card_text_and_live_log_formatting():
    ui = TelegramBotApi('mock_token')

    text = ui.launch_card_text(
        stage='POWERING_ON',
        elapsed_seconds=15,
        session_status='unknown',
        system_status='offline',
        target_mode=None,
    )
    assert '<b>Запуск компьютера</b>' in text
    assert '⏱ 15 сек' in text
    assert 'Ожидание выбора режима' in text
    assert '<blockquote>' in text
    assert '</blockquote>' in text
    assert '00:00 · Подача питания (ESP8266)' in text
    assert 'Загрузка Windows (⏱ 15с)' in text
    assert '--:-- · Экран входа Windows' in text

    text_online = ui.launch_card_text(
        stage='UNLOCKING',
        elapsed_seconds=28,
        session_status='login_screen',
        system_status='online',
        target_mode='workspace',
        target_detail='INCY VPN',
        unlock_attempts=1,
        win_online_elapsed=20,
        logon_ready_elapsed=24,
    )
    assert 'Разблок + Сетап (INCY VPN)' in text_online
    assert '00:20 · Windows в сети (System Agent)' in text_online
    assert '00:24 · Экран входа готов' in text_online
    assert 'Ввод пароля в Windows...' in text_online
    assert 'Рабочий сетап (INCY VPN)' in text_online
    assert 'Запуск начнётся сразу после разблокировки' in text_online

    text_delayed = ui.launch_card_text(
        stage='POWERING_ON',
        elapsed_seconds=65,
        session_status='unknown',
        system_status='offline',
        is_delayed=True,
    )
    assert 'Загрузка занимает больше минуты' in text_delayed
    assert 'аварийное выключение' in text_delayed


def test_launch_card_markup_hero_layout_and_emergency():
    ui = TelegramBotApi('mock_token')

    markup = ui.launch_card_markup(stage='POWERING_ON', target_mode=None, show_emergency=False)
    rows = markup['inline_keyboard']
    assert len(rows) == 3
    assert rows[0][0]['text'] == 'Разблок + Сетап'
    assert rows[0][0]['style'] == 'success'
    assert rows[1][0]['text'] == 'Только разблок'
    assert rows[1][1]['text'] == 'Разблок + Игра'
    assert rows[2][0]['text'] == 'Без разблокировки'
    assert rows[2][0]['style'] == 'danger'

    markup_delayed = ui.launch_card_markup(stage='POWERING_ON', target_mode=None, show_emergency=True)
    rows_delayed = markup_delayed['inline_keyboard']
    assert len(rows_delayed) == 4
    assert rows_delayed[3][0]['text'] == 'Аварийное выключение'
    assert rows_delayed[3][0]['style'] == 'danger'
    assert rows_delayed[3][0]['callback_data'] == 'emergency_power_off'


def test_power_menu_markup_contextual_emergency():
    ui = TelegramBotApi('mock_token')

    normal_markup = ui.power_menu_markup(session='unlocked', system_status='online')
    normal_callbacks = [btn['callback_data'] for row in normal_markup['inline_keyboard'] for btn in row]
    assert 'emergency_power_off' not in normal_callbacks

    unreachable_state = MagicMock()
    unreachable_state.state = 'UNREACHABLE'
    unreach_markup = ui.power_menu_markup(pc_state=unreachable_state, session='unknown', system_status='offline')
    unreach_callbacks = [btn['callback_data'] for row in unreach_markup['inline_keyboard'] for btn in row]
    assert 'emergency_power_off' in unreach_callbacks

    booting_state = MagicMock()
    booting_state.state = 'BOOTING'
    booting_markup = ui.power_menu_markup(pc_state=booting_state, session='unknown', system_status='offline')
    booting_callbacks = [btn['callback_data'] for row in booting_markup['inline_keyboard'] for btn in row]
    assert 'emergency_power_off' in booting_callbacks


def test_emergency_power_off_callback_flow():
    app = MockApp()

    handled = route_power_callback(app, 'emergency_power_off', 123456, 1001, 'cb_emerg')
    assert handled is True
    assert app.ui.answer_callback.called
    assert app.ui.send_message.called
    prompt_text = app.ui.send_message.call_args[0][1]
    assert 'Аварийное выключение ПК' in prompt_text
    assert '5 секунд' in prompt_text
    confirm_markup = app.ui.send_message.call_args[1]['reply_markup']
    assert confirm_markup['inline_keyboard'][0][0]['callback_data'] == 'emergency_power_off_confirm'

    app.ui.send_message.reset_mock()
    app.ui.answer_callback.reset_mock()

    with patch.object(app.power_flow, 'reset_flow') as mock_reset:
        handled_confirm = route_power_callback(app, 'emergency_power_off_confirm', 123456, 1001, 'cb_emerg_conf')
        assert handled_confirm is True
        assert app.mqtt.publish_esp_action.call_count >= 2
        calls = [c[0][0] for c in app.mqtt.publish_esp_action.call_args_list]
        assert 'FORCE_OFF' in calls
        assert 'LED_OFF' in calls
        assert mock_reset.called
        confirm_text = app.ui.send_message.call_args[0][1]
        assert 'Аварийное выключение выполнено' in confirm_text


def test_emergency_power_off_offline_esp_protection():
    app = MockApp()
    app.mqtt.esp_status = 'offline'

    handled = route_power_callback(app, 'emergency_power_off', 123456, 1001, 'cb_emerg_offline')
    assert handled is True
    app.ui.answer_callback.assert_called_with('cb_emerg_offline', '❌ Модуль питания (ESP8266) не в сети!', alert=True)
    assert not app.mqtt.publish_esp_action.called


def test_power_flow_tick_updates_launch_card():
    ui = TelegramBotApi('mock_token')
    ui.send_message = MagicMock(return_value={'message_id': 1001})
    mqtt = MagicMock()
    mqtt.system_status = 'offline'
    mqtt.system_session = 'unknown'

    with patch.object(PowerFlowManager, 'load_state'), patch.object(PowerFlowManager, 'save_state'):
        flow = PowerFlowManager(ui, mqtt)
        flow.stage = PowerFlowStage.POWERING_ON
        flow.chat_id = 123456
        flow.message_id = 1001
        flow.created_at = time.time() - 3.0
        flow.last_card_update_at = time.time() - 3.0

        ui.send_message.reset_mock()
        flow.tick(now=time.time())
        assert ui.send_message.called
        text = ui.send_message.call_args[0][1]
        assert 'Запуск компьютера' in text
        assert '⏱ 3 сек' in text
