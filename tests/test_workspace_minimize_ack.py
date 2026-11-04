import threading
from unittest.mock import MagicMock

import pytest

from vds_bot.bot import PcControlBotApp


@pytest.mark.parametrize("minimized", [True, False, None])
def test_workspace_uses_minimize_result_from_execution_ack(minimized):
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.ui.send_message.return_value = {"message_id": 1}
    app.mqtt = MagicMock()
    app.mqtt.desktop_status = "online"
    app.mqtt.publish_desktop_action.side_effect = ["workspace-1", "minimize-1"]
    app.power_flow = MagicMock()
    app.physical_btn = MagicMock()
    app.save_state = MagicMock()
    app.workspace_cancel_event = threading.Event()
    app.happ_ready_event = threading.Event()
    app.workspace_executed_event = threading.Event()
    app.minimize_ack_event = threading.Event()
    app.pending_minimize_cmd_id = None
    app.active_workspace_cmd_id = None
    app.workspace_card_msg_id = None
    app.app_statuses = {}
    published = threading.Event()

    def publish(action, *args, **kwargs):
        if action == "launch_app":
            published.set()
            return "workspace-1"
        return "minimize-1"

    app.mqtt.publish_desktop_action.side_effect = publish
    runner = threading.Thread(target=app.run_workspace_setup_steps, args=(123, None, "ws-1"), daemon=True)
    runner.start()
    try:
        assert published.wait(2)
        # Wait for assignment of the command ID before dispatching its ACKs.
        for _ in range(200):
            if app.active_workspace_cmd_id == "workspace-1":
                break
            threading.Event().wait(0.01)
        assert app.active_workspace_cmd_id == "workspace-1"
        for step in app.current_workspace_steps:
            app._on_mqtt_event("ack", {"id": "workspace-1", "result": "progress", "data": {"step_id": step["id"], "state": "verified"}})
        data = {} if minimized is None else {"desktop_minimized": minimized}
        app._on_mqtt_event("ack", {"id": "workspace-1", "result": "executed", "data": data})
        if minimized is not True:
            for _ in range(200):
                if app.pending_minimize_cmd_id == "minimize-1":
                    break
                threading.Event().wait(0.01)
            assert app.pending_minimize_cmd_id == "minimize-1"
            app._on_mqtt_event("ack", {"id": "minimize-1", "result": "executed", "data": {}})
        runner.join(timeout=2)
        assert not runner.is_alive()
        actions = [call.args[0] for call in app.mqtt.publish_desktop_action.call_args_list]
        assert actions == (["launch_app"] if minimized is True else ["launch_app", "minimize_all"])
        assert any("Рабочий сетап успешно запущен" in call.args[1] for call in app.ui.send_message.call_args_list)
    finally:
        app.workspace_cancel_event.set()
        runner.join(timeout=2)
