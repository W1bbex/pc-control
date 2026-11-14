import re
import sys


def _extract_function_body(content: str, func_name: str) -> str | None:
    match = re.search(rf"\bvoid\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{", content)
    if not match:
        return None
    start = match.end()
    depth = 1
    idx = start
    while idx < len(content) and depth > 0:
        ch = content[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        idx += 1
    if depth != 0:
        return None
    return content[start : idx - 1]


def check_timeouts(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    body = _extract_function_body(content, "connectMqtt")
    if body is None:
        print("ERROR: connectMqtt() function body not found")
        sys.exit(1)

    stop_pos = body.find("secureClient.stop();")
    timeout_pos = body.find("secureClient.setTimeout(3000);")
    connect_pos = body.find("mqtt.connect(")

    if stop_pos == -1:
        print("ERROR: secureClient.stop() not found inside connectMqtt()")
        sys.exit(1)

    if timeout_pos == -1:
        print("ERROR: secureClient.setTimeout(3000); not found inside connectMqtt()")
        sys.exit(1)

    if connect_pos == -1:
        print("ERROR: mqtt.connect( not found inside connectMqtt()")
        sys.exit(1)

    if not (stop_pos < timeout_pos < connect_pos):
        print(f"ERROR: Invalid call order inside connectMqtt(). stop={stop_pos}, timeout={timeout_pos}, connect={connect_pos}")
        sys.exit(1)

    print("Static check PASSED: secureClient.setTimeout(3000) is correctly placed between stop() and connect() inside connectMqtt().")
    sys.exit(0)


if __name__ == "__main__":
    check_timeouts(sys.argv[1])
